# Phase 1 — Disassembler: notes & first findings

Tool: `emu/unsp_disasm.py` — a faithful unSP **ISA 1.3** disassembler (Python port
of MAME's GPL-2.0 unSP dasm by Segher Boessenkool / David Haywood), targeting
`m_iso = 13` to match the Furby's `xasm16 -t5` / `CurIsa=ISA13` build.

Run:
```
python3 emu/unsp_disasm.py <file> --off <byteoff> --base <wordaddr> --count N
python3 emu/unsp_disasm.py <file> --map        # whole-image static access map
python3 emu/unsp_disasm.py --selftest x        # decode sanity checks
```

## Validated against the real firmware

`GameCode.bin` layout:
- **0x00–0x1f (bytes):** container header — `"PGpssiipps"` signature + version words.
- **byte 0x20 (= word 0x10):** the **reset/init routine** begins. Disassembles into
  coherent, textbook GPL16250 startup:

```
int off                              ; disable interrupts at reset
sp = 0x6fe9                          ; stack at top of 28K-word SRAM (0x0000-0x6FFF)  <-- matches datasheet
r1 = [0x782d] & 0xfff0 | 0x05 ...    ; read-modify-write system control reg
[0x7874]=0 [0x787c]=0 [0x787e]=0 ... ; clear control regs
r1 = [0x780f] & 0x07 ; je 0x3e       ; poll status reg in a loop (PLL/clock lock)
```

The `sp = 0x6fe9` landing exactly at the top of the datasheet's 28K-word SRAM is
strong proof the decoder is correct — random/misaligned bytes don't do that.

## Memory-mapped I/O = 0x7000–0x7FFF

Whole-image access map: **4303** distinct static addresses; **337** of them in
`0x7000–0x7FFF`, the GPL16250 I/O page. Busiest peripheral registers:

| addr | R | W | likely block (to confirm vs MAME gpl16250) |
|---|---|---|---|
| `0x7810` | 49 | 101 | system / clock control |
| `0x7817` | 26 | 1 | status / poll (PLL lock) |
| `0x7a34` `0x7abe` `0x7abf` `0x7b85` | high | high | **SPU (audio)** |
| `0x79d2-0x79e8` | high | high | **DMA** (feeds SPU) |
| `0x78e0` `0x78f1` | ~ | ~ | **interrupt controller** |

## Recursive-descent tracer — `emu/unsp_trace.py`

Follows control flow (branch both ways, call = target+return, goto = target only,
stop at ret / unresolved computed jumps) so only real code is decoded.

Findings:
- **Reset entry is word `0x20`** (byte 0x40); header is 0x40 bytes. (Words
  0x10–0x1f are header tail — decoding them as code was noise.)
- Descent from reset alone reaches only **0.5%** — this firmware dispatches heavily
  through **jump tables / `goto mr`** (≈7k unresolved computed jumps), which static
  descent can't follow.
- **Call-target harvesting** (linear scan for every 2-word `call A22`, seed descent
  from all 558 of them) lifts coverage to **20.3%**: **88,506 instructions across
  756 functions**. The remaining ~80% is audio/graphics/table *data*, as expected.
- Real code spans `0x000020`–`~0x06Cxxx`; main logic lives up at `0x05xxxx–0x06xxxx`
  (reset stub inits clocks then jumps there).

### Peripheral registers — CONFIRMED against MAME `generalplus_gpl162xx_soc.cpp`

Names baked into `emu/gpl16250_regs.py` (so we never need the MAME tree again).
Cross-checked: the reset routine writes 0x7803/0x780a/0x780f/0x7810/0x782d/0x787c/
0x7888 exactly as these names predict — decoder + map both validated.

| range / reg | name | drives |
|---|---|---|
| `0x7000–0x70ff` | PPU tilemap/sprite control | **the eyes** (PPU, not a dumb FB) |
| `0x7300–0x73ff` | PPU palette RAM (64-colour) | **eye colours** |
| `0x7400–0x77ff` | PPU sprite RAM | **eye cels/sprites** |
| `0x7803` | `P_SystemControl` | system |
| `0x780a` / `0x780b` | `P_Watchdog_Ctrl` / `_Clear` | **watchdog (Risk R7)** |
| `0x780f` | `P_PowerState` | reset poll-loop (PLL ready) |
| `0x7810` | `P_BankSwitch_Ctrl` | **memory banking** (hottest reg) |
| `0x7817` `0x7818` | `P_PLLChange` `P_PLLCLKWait` | clock |
| `0x7860–0x7883` | `P_IOA..IOE_*` | **GPIO** = motor + sensors + LCD |
| `0x78a0–0x78a8` | `P_INT_*` | interrupt controller |
| `0x78b0–0x78b8` | `P_TimeBaseA/B/C` | **32768Hz heartbeat** |
| `0x78c0–0x78d8` | `P_TimerA..D_Ctrl` | timers (motor PWM) |
| `0x78fb` | `P_DAC_PGA` | audio DAC |
| `0x7960–0x7962` | `P_ADC/MADC_*` | ADC = mic / light / tilt |
| `0x7a80–0x7a9f` | `DMA_Ch0..3_Params` | system DMA |
| `0x7abe` `0x7abf` | `P_DMA_MemType` `_Status` | DMA |
| `0x7b80–0x7bbf` | `SPU_Audio_Ctrl` | **16-ch SPU (sound)** |

**Unmapped / Furby-specific (hot but not in MAME's base chip — investigate):**
`0x7a34` (20 writes — top of the whole map), the `0x79d2–0x79e8` cluster, `0x7bf0`.
These are where the GPL16258 or Furby board diverges from the generic gcm394.

Key reframe: **the eyes are a PPU** (tilemap + sprite RAM + palette), which is
exactly why the DLC format carries CEL/SPR/PAL sections — they load straight into
`0x7400`/`0x7000`/`0x7300`. Phase 4's display model is a PPU, not a framebuffer.

Run:
```
python3 emu/unsp_trace.py <GameCode.bin> --harvest --map      # trustworthy I/O map
python3 emu/unsp_trace.py <GameCode.bin> --harvest --funcs    # 756 function starts
python3 emu/unsp_trace.py <GameCode.bin> --harvest --unresolved  # jump tables to map
python3 emu/unsp_trace.py <GameCode.bin> --list 0x20 --count 40  # annotated code/data
```

## Caveats / next steps
- **Confirm register labels** against MAME `generalplus_gpl16250` I/O definitions
  (turn "SPU?"/"DMA?" into named registers) — the block map above is inference.
- **Resolve jump tables** (the 7k computed jumps) to push coverage past 20% and
  map the action-dispatch machinery.
- Feed this into Phase 2: the I/O map tells the CPU bus exactly which addresses
  route to peripheral models vs plain RAM. `0x7000–0x7FFF` = MMIO; below = RAM.
