#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeneralPlus GPL16258 (GPL1625x / gcm394-family) memory-mapped I/O register map.

Names taken from MAME's `generalplus_gpl162xx_soc.cpp` (Segher / Haywood et al.,
GPL-2.0) and the GeneralPlus `P_*` register naming, cross-checked against what the
Furby's GameCode.bin actually reads/writes (the reset routine hits 0x7803 / 0x780a
/ 0x780f / 0x7810 / 0x782d / 0x787c / 0x7888 exactly as named here).

This is baked in so the disassembler/tracer — and later the emulator's memory bus —
can name registers without pulling the whole MAME tree again.

Everything is a *word* address. I/O lives in 0x7000-0x7FFF; below 0x7000 is RAM.
"""

from __future__ import annotations

# exact single-register names
REG_NAMES = {
    # --- system / clock / power / memory control (0x7800 block) ---
    0x7803: ("P_SystemControl", "sys"),
    0x7804: ("P_CLK_Ctrl0", "sys"),
    0x7807: ("P_Clock_Ctrl", "sys"),
    0x780a: ("P_Watchdog_Ctrl", "sys"),        # <-- Risk R7: service or it resets
    0x780b: ("P_Watchdog_Clear", "sys"),
    0x780c: ("P_WaitMode_Enter", "sys"),
    0x780f: ("P_PowerState", "sys"),           # reset polls this (PLL/power ready)
    0x7810: ("P_BankSwitch_Ctrl", "sys"),      # memory banking (very hot)
    0x7816: ("P_Unk_7816", "sys"),
    0x7817: ("P_PLLChange", "sys"),
    0x7818: ("P_PLLCLKWait", "sys"),
    0x7819: ("P_Cache_Ctrl", "sys"),
    0x782d: ("P_RAW_WAR_782d", "sys"),         # touched on startup
    0x7835: ("P_MCS0_Page", "sys"),
    # memory drive / delay (P_MEM_*)
    0x7888: ("P_MEM_DRV", "sys"),
    0x7889: ("P_MEM_DLY0", "sys"), 0x788a: ("P_MEM_DLY1", "sys"),
    0x788b: ("P_MEM_DLY2", "sys"), 0x788c: ("P_MEM_DLY3", "sys"),
    0x788d: ("P_MEM_DLY4", "sys"), 0x788e: ("P_MEM_DLY5", "sys"),
    0x788f: ("P_MEM_DLY6", "sys"),

    # --- GPIO ports A-E (motor, sensors, LCD lines) ---
    0x7860: ("P_IOA_Data", "gpio"), 0x7861: ("P_IOA_Buffer", "gpio"),
    0x7862: ("P_IOA_Dir", "gpio"),  0x7863: ("P_IOA_Attrib", "gpio"),
    0x7868: ("P_IOB_Data", "gpio"), 0x7869: ("P_IOB_Buffer", "gpio"),
    0x786a: ("P_IOB_Dir", "gpio"),  0x786b: ("P_IOB_Attrib", "gpio"),
    0x7870: ("P_IOC_Data", "gpio"), 0x7871: ("P_IOC_Buffer", "gpio"),
    0x7872: ("P_IOC_Dir", "gpio"),  0x7873: ("P_IOC_Attrib", "gpio"),
    0x7878: ("P_IOD_Data", "gpio"), 0x7879: ("P_IOD_Buffer", "gpio"),
    0x787a: ("P_IOD_Dir", "gpio"),  0x787b: ("P_IOD_Attrib", "gpio"),
    0x787c: ("P_IOD_Drv", "gpio"),
    0x7880: ("P_IOE_Data", "gpio"), 0x7881: ("P_IOE_Buffer", "gpio"),
    0x7882: ("P_IOE_Dir", "gpio"),  0x7883: ("P_IOE_Attrib", "gpio"),

    # --- interrupt controller ---
    0x78a0: ("P_INT_Status1", "intc"), 0x78a1: ("P_INT_Status2", "intc"),
    0x78a3: ("P_INT_Status3", "intc"),
    0x78a4: ("P_INT_Priority1", "intc"), 0x78a5: ("P_INT_Priority2", "intc"),
    0x78a6: ("P_INT_Priority3", "intc"), 0x78a8: ("P_MINT_Ctrl", "intc"),

    # --- TimeBase (32768Hz heartbeat) + Timers A-D ---
    0x78b0: ("P_TimeBaseA_Ctrl", "timer"), 0x78b1: ("P_TimeBaseB_Ctrl", "timer"),
    0x78b2: ("P_TimeBaseC_Ctrl", "timer"), 0x78b8: ("P_TimeBase_Reset", "timer"),
    0x78c0: ("P_TimerA_Ctrl", "timer"), 0x78c8: ("P_TimerB_Ctrl", "timer"),
    0x78d0: ("P_TimerC_Ctrl", "timer"), 0x78d8: ("P_TimerD_Ctrl", "timer"),

    # --- audio DAC channel / PGA ---
    0x78f0: ("P_CHA_Ctrl", "dac"),
    0x78fb: ("P_DAC_PGA", "dac"),

    # --- UART / RTC / SPI / ADC ---
    0x7904: ("P_UART_Status", "uart"),
    0x7934: ("P_RTC_Ctrl", "rtc"), 0x7935: ("P_RTC_INT_Status", "rtc"),
    0x7936: ("P_RTC_INT_Ctrl", "rtc"),
    0x7942: ("P_SPI_TXData", "spi"), 0x7944: ("P_SPI_RXData", "spi"),
    0x7945: ("P_SPI_Misc_Ctrl", "spi"),
    0x7960: ("P_ADC_Setup", "adc"), 0x7961: ("P_MADC_Ctrl", "adc"),
    0x7962: ("P_MADC_Data", "adc"),                # mic / light / tilt sensor input

    # --- system DMA (feeds SPU/graphics) ---
    0x7abe: ("P_DMA_MemType", "dma"), 0x7abf: ("P_DMA_Status", "dma"),
}

# range-based regions (start, end_inclusive, name, group)
REG_RANGES = [
    (0x7000, 0x70ff, "PPU_Tilemap/Sprite_Ctrl", "ppu"),   # eyes: tilemap+sprite regs
    (0x7300, 0x73ff, "PPU_Palette_RAM", "ppu"),           # eyes: 64-colour palettes
    (0x7400, 0x77ff, "PPU_Sprite_RAM", "ppu"),            # eyes: sprite/cel data
    (0x7a80, 0x7a87, "DMA_Ch0_Params", "dma"),
    (0x7a88, 0x7a8f, "DMA_Ch1_Params", "dma"),
    (0x7a90, 0x7a97, "DMA_Ch2_Params", "dma"),
    (0x7a98, 0x7a9f, "DMA_Ch3_Params", "dma"),
    (0x7b80, 0x7bbf, "SPU_Audio_Ctrl", "spu"),            # 16-channel sound unit
]

GROUP_DESC = {
    "sys": "system/clock/power/memory", "gpio": "GPIO (motor+sensors+LCD lines)",
    "intc": "interrupt controller", "timer": "timers / 32768Hz timebase heartbeat",
    "dac": "audio DAC", "adc": "ADC (mic/light/tilt)", "dma": "system DMA",
    "spu": "SPU (16-ch audio)", "ppu": "PPU (eye tilemap/sprite/palette)",
    "uart": "UART", "rtc": "real-time clock", "spi": "SPI bus", "io": "unclassified I/O",
}


def reg_name(addr):
    """Return (name, group) for a word address, or (None, None) if not I/O."""
    if addr in REG_NAMES:
        return REG_NAMES[addr]
    for lo, hi, name, grp in REG_RANGES:
        if lo <= addr <= hi:
            return (name, grp)
    if 0x7000 <= addr <= 0x7fff:
        return (f"io_{addr:04x}", "io")     # inside I/O page but unnamed
    return (None, None)


def is_mmio(addr):
    return 0x7000 <= addr <= 0x7fff


if __name__ == "__main__":
    # quick dump grouped by function
    from collections import defaultdict
    by_grp = defaultdict(list)
    for a, (n, g) in REG_NAMES.items():
        by_grp[g].append((a, n))
    for lo, hi, n, g in REG_RANGES:
        by_grp[g].append((lo, f"{n} (0x{lo:04x}-0x{hi:04x})"))
    for g in sorted(by_grp):
        print(f"\n[{g}] {GROUP_DESC.get(g,'')}")
        for a, n in sorted(by_grp[g]):
            print(f"  0x{a:04x}  {n}")
